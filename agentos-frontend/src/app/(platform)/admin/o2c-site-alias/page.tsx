"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import {
  createServiceSite,
  listBillingClients,
  listO2CSites,
  listOpenO2CAttendanceRecon,
  resolveO2CAttendanceReconAndRerun,
  type BillingClientItem,
  type O2CAttendanceReconItem,
  type O2CSiteRow,
} from "@/lib/api";

/** Previous calendar month as YYYY-MM-DD (for optional contract-version check). */
function prevCalendarMonthRange(): { start: string; end: string } {
  const now = new Date();
  const firstPrev = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const yy = firstPrev.getFullYear();
  const mm = firstPrev.getMonth();
  const lastDay = new Date(yy, mm + 1, 0).getDate();
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    start: `${yy}-${pad(mm + 1)}-01`,
    end: `${yy}-${pad(mm + 1)}-${pad(lastDay)}`,
  };
}

export default function O2CSiteAliasAdminPage() {
  const [siteQuery, setSiteQuery] = useState("");
  const [reconQuery, setReconQuery] = useState("");
  const [siteLoading, setSiteLoading] = useState(false);
  const [items, setItems] = useState<O2CSiteRow[]>([]);
  const [reconLoading, setReconLoading] = useState(false);
  const [reconItems, setReconItems] = useState<O2CAttendanceReconItem[]>([]);
  const [selectedReconId, setSelectedReconId] = useState("");
  const [selectedSiteId, setSelectedSiteId] = useState("");
  const [aliasCode, setAliasCode] = useState("");
  const [aliasDisplay, setAliasDisplay] = useState("");
  const [sourceSystem, setSourceSystem] = useState("manual");
  const [autoRerun, setAutoRerun] = useState(true);
  const [useReconPeriod, setUseReconPeriod] = useState(false);
  const [allowAliasReassign, setAllowAliasReassign] = useState(false);
  const [resolutionNotes, setResolutionNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [showRawResult, setShowRawResult] = useState(false);
  const [resolveMode, setResolveMode] = useState<"map" | "create">("map");
  const [newSiteKey, setNewSiteKey] = useState("");
  const [newSiteDisplay, setNewSiteDisplay] = useState("");
  const [newSiteBillingClientId, setNewSiteBillingClientId] = useState("");
  const [createSiteBusy, setCreateSiteBusy] = useState(false);
  const [createSiteResult, setCreateSiteResult] = useState<Record<string, unknown> | null>(null);
  const [billingClients, setBillingClients] = useState<BillingClientItem[]>([]);
  const [billingClientLoading, setBillingClientLoading] = useState(false);
  const [billingClientQuery, setBillingClientQuery] = useState("");
  const [showBillingClientDropdown, setShowBillingClientDropdown] = useState(false);
  const [templateSourceSiteId, setTemplateSourceSiteId] = useState("");
  const [templateSiteQuery, setTemplateSiteQuery] = useState("");
  const [templateSiteResults, setTemplateSiteResults] = useState<O2CSiteRow[]>([]);
  const [templateSiteLoading, setTemplateSiteLoading] = useState(false);
  const [createAutoMis, setCreateAutoMis] = useState(true);
  const [requireSameTermsForClone, setRequireSameTermsForClone] = useState(false);
  const [clonePeriodStart, setClonePeriodStart] = useState(() => prevCalendarMonthRange().start);
  const [clonePeriodEnd, setClonePeriodEnd] = useState(() => prevCalendarMonthRange().end);

  const selectedRecon = useMemo(
    () => reconItems.find((x) => x.id === selectedReconId) ?? null,
    [reconItems, selectedReconId]
  );
  const selectedSite = useMemo(
    () => items.find((x) => x.id === selectedSiteId) ?? null,
    [items, selectedSiteId]
  );
  const selectedBillingClient = useMemo(
    () => billingClients.find((bc) => bc.id === newSiteBillingClientId) ?? null,
    [billingClients, newSiteBillingClientId]
  );
  const filteredReconItems = useMemo(() => {
    const q = reconQuery.trim().toLowerCase();
    if (!q) return reconItems;
    return reconItems.filter((r) =>
      [
        r.client_site_key,
        r.reason_code,
        r.detail,
        r.billing_period_start,
        r.billing_period_end,
      ]
        .join(" ")
        .toLowerCase()
        .includes(q)
    );
  }, [reconItems, reconQuery]);

  useEffect(() => {
    void loadRecon();
    void onBillingClientSearch("");
  }, []);

  /** After refresh, drop stale selection so link/create flows never use a missing recon id. */
  useEffect(() => {
    if (reconItems.length === 0) {
      if (selectedReconId) {
        setSelectedReconId("");
        setAliasCode("");
        setAliasDisplay("");
        setNewSiteKey("");
        setNewSiteDisplay("");
      }
      return;
    }
    if (!selectedReconId || !reconItems.some((r) => r.id === selectedReconId)) {
      const first = reconItems[0];
      setSelectedReconId(first.id);
      setAliasCode(first.client_site_key);
      setAliasDisplay(first.client_site_key);
      setNewSiteKey(first.client_site_key);
      setNewSiteDisplay(first.client_site_key);
    }
  }, [reconItems, selectedReconId]);

  useEffect(() => {
    setTemplateSourceSiteId("");
    setTemplateSiteResults([]);
  }, [newSiteBillingClientId]);

  async function loadRecon() {
    setReconLoading(true);
    setError("");
    try {
      const out = await listOpenO2CAttendanceRecon(300);
      setReconItems(out.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t load the list. Try Refresh.");
    } finally {
      setReconLoading(false);
    }
  }

  async function onSiteSearch() {
    setSiteLoading(true);
    setError("");
    try {
      const out = await listO2CSites(siteQuery, 200);
      setItems(out.items);
      setSelectedSiteId((prev) => (prev && out.items.some((x) => x.id === prev) ? prev : ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load sites");
    } finally {
      setSiteLoading(false);
    }
  }

  async function onTemplateSiteSearch() {
    if (!newSiteBillingClientId.trim()) {
      setError("Choose a billing client before searching for a template site");
      return;
    }
    setTemplateSiteLoading(true);
    setError("");
    try {
      const out = await listO2CSites(templateSiteQuery, 200);
      const filtered = out.items.filter((s) => s.billing_client_id === newSiteBillingClientId.trim());
      setTemplateSiteResults(filtered);
      if (templateSourceSiteId && !filtered.some((s) => s.id === templateSourceSiteId)) {
        setTemplateSourceSiteId("");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load template sites");
      setTemplateSiteResults([]);
    } finally {
      setTemplateSiteLoading(false);
    }
  }

  async function onBillingClientSearch(q: string) {
    setBillingClientLoading(true);
    try {
      const out = await listBillingClients(q, 50);
      setBillingClients(out.items);
    } catch {
      setBillingClients([]);
    } finally {
      setBillingClientLoading(false);
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedReconId || !selectedRecon) {
      setError("Pick a row from the list above first");
      return;
    }
    if (!selectedSiteId) {
      setError("Choose which site this name should link to");
      return;
    }
    if (!aliasCode.trim()) {
      setError("Enter the attendance name exactly as it appears in the file");
      return;
    }
    setSubmitting(true);
    setError("");
    setResult(null);
    try {
      const out = await resolveO2CAttendanceReconAndRerun({
        recon_id: selectedReconId,
        service_site_id: selectedSiteId,
        alias_code: aliasCode.trim(),
        alias_display: aliasDisplay.trim() || undefined,
        source_system: sourceSystem.trim() || "manual",
        resolution_notes: resolutionNotes.trim() || undefined,
        auto_run_previous_month_mis: autoRerun,
        use_recon_period: useReconPeriod,
        allow_alias_reassign: allowAliasReassign,
      });
      setResult(out as Record<string, unknown>);
      await loadRecon();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function onCreateSite(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedReconId || !selectedRecon) {
      setError("Pick a row under Needs attention — new sites can only be created for an open attendance issue.");
      return;
    }
    const attendanceLabel = selectedRecon.client_site_key.trim();
    if (!attendanceLabel) {
      setError("The selected row has no attendance name.");
      return;
    }
    if (!newSiteKey.trim() || !newSiteBillingClientId.trim()) {
      setError("Site code and customer are required");
      return;
    }
    setCreateSiteBusy(true);
    setError("");
    setCreateSiteResult(null);
    try {
      const pm = prevCalendarMonthRange();
      const out = await createServiceSite({
        site_key: newSiteKey.trim(),
        display_name: newSiteDisplay.trim() || undefined,
        billing_client_id: newSiteBillingClientId.trim(),
        recon_id: selectedReconId,
        alias_code: attendanceLabel,
        auto_run_previous_month_mis: createAutoMis,
        ...(templateSourceSiteId.trim()
          ? { clone_rate_lines_from_site_id: templateSourceSiteId.trim() }
          : {}),
        require_same_terms_for_period: requireSameTermsForClone,
        ...(requireSameTermsForClone
          ? {
              clone_terms_period_start: clonePeriodStart.trim() || pm.start,
              clone_terms_period_end: clonePeriodEnd.trim() || pm.end,
            }
          : {}),
      });
      setCreateSiteResult(out as unknown as Record<string, unknown>);
      setResolveMode("map");
      const bcName = selectedBillingClient?.name ?? "";
      const newRow: O2CSiteRow = {
        id: out.service_site_id,
        billing_client_id: newSiteBillingClientId.trim(),
        billing_client_name: bcName,
        site_key: out.site_key,
        display_name: (out.display_name || out.site_key).trim(),
        canonical_name: out.site_key,
      };
      setItems((prev) => {
        const rest = prev.filter((p) => p.id !== newRow.id);
        return [newRow, ...rest];
      });
      setSelectedSiteId(out.service_site_id);
      await loadRecon();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create site");
    } finally {
      setCreateSiteBusy(false);
    }
  }

  function onSelectRecon(rec: O2CAttendanceReconItem) {
    setSelectedReconId(rec.id);
    setAliasCode(rec.client_site_key);
    setAliasDisplay(rec.client_site_key);
    setNewSiteKey(rec.client_site_key);
    setNewSiteDisplay(rec.client_site_key);
    setResult(null);
    setCreateSiteResult(null);
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold mb-1">Fix attendance site names</h1>
        <p className="text-[var(--text-secondary)] text-sm leading-relaxed max-w-3xl">
          When attendance shows a site name we don&apos;t recognise, either{" "}
          <strong className="font-semibold text-[var(--text-primary)]">link it to a site we already have</strong> or{" "}
          <strong className="font-semibold text-[var(--text-primary)]">add a new site</strong>. We can refresh the billing
          draft in the same step.
        </p>
      </div>

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold">Needs attention</div>
          <button
            type="button"
            onClick={loadRecon}
            disabled={reconLoading}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors ${
              reconLoading
                ? "bg-[var(--accent-blue)] text-white cursor-wait"
                : "bg-[var(--bg-elev)] border border-[var(--border)] text-[var(--text-primary)] hover:border-[var(--border-active)]/70 hover:bg-white"
            } disabled:opacity-70`}
          >
            {reconLoading ? "Loading..." : "Refresh"}
          </button>
        </div>
        <div className="mb-3">
          <input
            value={reconQuery}
            onChange={(e) => setReconQuery(e.target.value)}
            placeholder="Search by site name, period, or reason"
            className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
          />
        </div>
        <div className="max-h-64 overflow-auto border border-[var(--border)] rounded-lg">
          {filteredReconItems.length === 0 ? (
            <div className="p-3 text-sm text-[var(--text-muted)]">All clear — nothing waiting in this list.</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="bg-[var(--bg-elev)]">
                <tr>
                  <th className="text-left p-2">Site label</th>
                  <th className="text-left p-2">Period</th>
                  <th className="text-left p-2">Reason</th>
                  <th className="text-left p-2">Detail</th>
                </tr>
              </thead>
              <tbody>
                {filteredReconItems.map((rec) => (
                  <tr
                    key={rec.id}
                    onClick={() => onSelectRecon(rec)}
                    className={`cursor-pointer border-t border-[var(--border)] ${
                      selectedReconId === rec.id ? "bg-white/10" : "hover:bg-white/5"
                    }`}
                  >
                    <td className="p-2">{rec.client_site_key}</td>
                    <td className="p-2">
                      {rec.billing_period_start} to {rec.billing_period_end}
                    </td>
                    <td className="p-2">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium ${
                        rec.reason_code === "unresolved_site"
                          ? "bg-red-100 text-red-700"
                          : rec.reason_code === "no_billable_contract"
                            ? "bg-amber-100 text-amber-700"
                            : rec.reason_code === "no_rate_lines"
                              ? "bg-orange-100 text-orange-700"
                              : "bg-gray-100 text-gray-700"
                      }`}>
                        {rec.reason_code === "unresolved_site" ? "Unmapped Site" :
                         rec.reason_code === "no_billable_contract" ? "No Contract" :
                         rec.reason_code === "terms_ambiguous" ? "Multiple approved contracts" :
                         rec.reason_code === "no_rate_lines" ? "Needs site rates" :
                         rec.reason_code}
                      </span>
                    </td>
                    <td className="p-2 max-w-[200px] truncate text-[var(--text-muted)]" title={rec.detail}>
                      {rec.detail || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
        <div className="text-sm font-semibold mb-3">What do you want to do?</div>
        <div className="flex gap-0 mb-3">
          <button
            type="button"
            onClick={() => {
              setError("");
              setResolveMode("map");
            }}
            className={`flex-1 px-4 py-2 text-sm font-semibold rounded-l-lg border transition-colors ${
              resolveMode === "map"
                ? "bg-[var(--accent-blue)] text-white border-[var(--accent-blue)]"
                : "bg-[var(--bg-elev)] text-[var(--text-secondary)] border-[var(--border)] hover:bg-white/5"
            }`}
          >
            Link to existing site
          </button>
          <button
            type="button"
            onClick={() => {
              setError("");
              setResolveMode("create");
            }}
            className={`flex-1 px-4 py-2 text-sm font-semibold rounded-r-lg border border-l-0 transition-colors ${
              resolveMode === "create"
                ? "bg-[var(--accent-green)] text-white border-[var(--accent-green)]"
                : "bg-[var(--bg-elev)] text-[var(--text-secondary)] border-[var(--border)] hover:bg-white/5"
            }`}
          >
            Add new site
          </button>
        </div>

        {resolveMode === "create" && (
          <div
            className="mb-4 rounded-lg border border-amber-200/90 bg-amber-50/60 px-3 py-3 text-sm text-amber-950 leading-relaxed"
            role="note"
          >
            <p className="font-semibold text-amber-950 mb-1">Attendance issue required</p>
            <p className="text-amber-950/95">
              You must <strong className="text-amber-950">select one row</strong> in{" "}
              <strong className="text-amber-950">Needs attention</strong> above. We create the billing site from your
              site code and name, and we link the <strong className="text-amber-950">exact name from that row</strong>{" "}
              (as it appears in attendance) to the new site. If the site already exists, use{" "}
              <strong className="text-amber-950">Link to existing site</strong> instead.
            </p>
          </div>
        )}

        {resolveMode === "map" && (
          <div>
            <div className="flex gap-2">
              <input
                value={siteQuery}
                onChange={(e) => setSiteQuery(e.target.value)}
                placeholder="Search by site name, code, or customer"
                className="flex-1 px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
              />
              <button
                type="button"
                onClick={onSiteSearch}
                disabled={siteLoading}
                className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${
                  siteLoading
                    ? "bg-[var(--accent-blue)] text-white cursor-wait"
                    : "bg-[var(--accent-green)] text-white hover:brightness-95"
                } disabled:opacity-70`}
              >
                {siteLoading ? "Searching..." : "Search"}
              </button>
            </div>
            <p className="text-xs text-[var(--text-muted)] mt-2">
              Pick the site that should receive attendance for the name you selected in the list.
            </p>
          </div>
        )}

        {resolveMode === "create" && (
          <form onSubmit={onCreateSite} className="border border-[var(--border)] rounded-lg p-3 bg-[var(--bg-elev)] space-y-2">
            {selectedRecon ? (
              <div className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-2 text-sm mb-2">
                <span className="text-xs text-[var(--text-muted)] font-semibold uppercase tracking-wide">
                  Attendance name we will link
                </span>
                <p className="font-medium text-[var(--text-primary)] mt-0.5">{selectedRecon.client_site_key}</p>
              </div>
            ) : (
              <div className="rounded-md border border-amber-200/80 bg-amber-50/50 px-3 py-2 text-sm text-amber-950 mb-2">
                Select a row in <strong>Needs attention</strong> to continue.
              </div>
            )}
            <div className="text-sm font-semibold mb-2 text-[var(--text-primary)]">New site details</div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
              <div>
                <label className="block text-xs text-[var(--text-muted)] mb-0.5">Site code</label>
                <input
                  value={newSiteKey}
                  onChange={(e) => setNewSiteKey(e.target.value)}
                  placeholder={selectedRecon?.client_site_key || "e.g. tcs_noida_phase2"}
                  className="w-full px-2.5 py-1.5 rounded-md bg-[var(--bg-primary)] border border-[var(--border)] text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-[var(--text-muted)] mb-0.5">Site name</label>
                <input
                  value={newSiteDisplay}
                  onChange={(e) => setNewSiteDisplay(e.target.value)}
                  placeholder="e.g. Acme Pune Office"
                  className="w-full px-2.5 py-1.5 rounded-md bg-[var(--bg-primary)] border border-[var(--border)] text-sm"
                />
              </div>
            </div>
            <div className="relative">
              <label className="block text-xs text-[var(--text-muted)] mb-0.5">Customer (who we bill)</label>
              {newSiteBillingClientId && selectedBillingClient ? (
                <div className="flex items-center gap-2">
                  <div className="flex-1 px-2.5 py-1.5 rounded-md bg-[var(--bg-primary)] border border-[var(--accent-green)]/40 text-sm font-medium">
                    {selectedBillingClient.name}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      setNewSiteBillingClientId("");
                      setBillingClientQuery("");
                      setTemplateSourceSiteId("");
                      setTemplateSiteResults([]);
                    }}
                    className="px-2.5 py-1.5 rounded-md border border-[var(--border)] text-xs hover:bg-white/5 transition-colors"
                  >
                    Change
                  </button>
                </div>
              ) : (
                <>
                  <input
                    value={billingClientQuery}
                    onChange={(e) => {
                      setBillingClientQuery(e.target.value);
                      setShowBillingClientDropdown(true);
                      void onBillingClientSearch(e.target.value);
                    }}
                    onFocus={() => setShowBillingClientDropdown(true)}
                    onBlur={() => setTimeout(() => setShowBillingClientDropdown(false), 150)}
                    placeholder="Type to search billing clients..."
                    className="w-full px-2.5 py-1.5 rounded-md bg-[var(--bg-primary)] border border-[var(--border)] text-sm"
                  />
                  {showBillingClientDropdown && (
                    <div className="absolute z-10 left-0 right-0 mt-1 max-h-48 overflow-auto rounded-md border border-[var(--border)] bg-[var(--bg-card)] shadow-lg">
                      {billingClientLoading && billingClients.length === 0 && (
                        <div className="px-3 py-2 text-xs text-[var(--text-muted)]">Searching...</div>
                      )}
                      {!billingClientLoading && billingClients.length === 0 && billingClientQuery.trim() && (
                        <div className="px-3 py-2 text-xs text-[var(--text-muted)]">No billing clients found</div>
                      )}
                      {billingClients.map((bc) => (
                        <button
                          key={bc.id}
                          type="button"
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => {
                            setNewSiteBillingClientId(bc.id);
                            setBillingClientQuery(bc.name);
                            setShowBillingClientDropdown(false);
                          }}
                          className="w-full text-left px-3 py-2 text-sm hover:bg-white/10 border-b border-[var(--border)] last:border-b-0"
                        >
                          {bc.name}
                        </button>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>

            <p className="text-sm leading-relaxed text-[var(--text-secondary)] border border-[var(--border)] rounded-lg px-3 py-2.5 bg-[var(--bg-primary)]">
              New sites start with <strong className="text-[var(--text-primary)]">shared</strong> pricing only. To match
              another location&apos;s rates, use the next section — same customer only.
            </p>

            <div className="space-y-2 border border-[var(--border)] rounded-lg p-3 bg-[var(--bg-primary)]">
              <div className="text-sm font-semibold text-[var(--text-primary)]">Copy rates from another site (optional)</div>
              <p className="text-xs text-[var(--text-muted)] leading-snug">
                Choose a similar site under this customer so nurses, guards, etc. line up the same way.
              </p>
              <div className="flex gap-2">
                <input
                  value={templateSiteQuery}
                  onChange={(e) => setTemplateSiteQuery(e.target.value)}
                  placeholder="Search site name or code"
                  className="flex-1 px-2.5 py-1.5 rounded-md bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
                />
                <button
                  type="button"
                  onClick={() => void onTemplateSiteSearch()}
                  disabled={templateSiteLoading || !newSiteBillingClientId.trim()}
                  className="px-3 py-1.5 rounded-md text-xs font-semibold bg-[var(--bg-elev)] border border-[var(--border)] hover:bg-white/5 disabled:opacity-50"
                >
                  {templateSiteLoading ? "…" : "Search"}
                </button>
              </div>
              {templateSiteResults.length > 0 && (
                <div className="max-h-36 overflow-auto rounded-md border border-[var(--border)] divide-y divide-[var(--border)]">
                  {templateSiteResults.map((s) => (
                    <label
                      key={s.id}
                      className={`flex items-start gap-2 px-2 py-1.5 cursor-pointer text-xs hover:bg-white/5 ${
                        templateSourceSiteId === s.id ? "bg-[var(--accent-green)]/10" : ""
                      }`}
                    >
                      <input
                        type="radio"
                        name="templateSite"
                        checked={templateSourceSiteId === s.id}
                        onChange={() => setTemplateSourceSiteId(s.id)}
                        className="mt-0.5"
                      />
                      <span className="min-w-0">
                        <span className="font-medium text-[var(--text-primary)]">{s.display_name || s.site_key}</span>
                        <span className="text-[var(--text-muted)] font-mono ml-1">{s.site_key}</span>
                      </span>
                    </label>
                  ))}
                </div>
              )}
              {templateSourceSiteId ? (
                <button type="button" onClick={() => setTemplateSourceSiteId("")} className="text-[10px] text-[var(--accent-blue)]">
                  Clear “copy from” site
                </button>
              ) : null}
              <label className="flex items-start gap-2 text-xs cursor-pointer leading-snug">
                <input
                  type="checkbox"
                  checked={requireSameTermsForClone}
                  onChange={(e) => setRequireSameTermsForClone(e.target.checked)}
                  className="mt-0.5 shrink-0"
                />
                <span>
                  Only copy if both sites are on the <strong className="font-medium text-[var(--text-primary)]">same contract</strong> for the dates below (advanced).
                </span>
              </label>
              {requireSameTermsForClone ? (
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-xs text-[var(--text-muted)] mb-0.5">From date</label>
                    <input
                      type="date"
                      value={clonePeriodStart}
                      onChange={(e) => setClonePeriodStart(e.target.value)}
                      className="w-full px-2 py-1 rounded-md border border-[var(--border)] text-xs bg-[var(--bg-elev)]"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-[var(--text-muted)] mb-0.5">To date</label>
                    <input
                      type="date"
                      value={clonePeriodEnd}
                      onChange={(e) => setClonePeriodEnd(e.target.value)}
                      className="w-full px-2 py-1 rounded-md border border-[var(--border)] text-xs bg-[var(--bg-elev)]"
                    />
                  </div>
                </div>
              ) : null}
            </div>

            <label className="flex items-start gap-2 text-sm leading-snug cursor-pointer">
              <input
                type="checkbox"
                checked={createAutoMis}
                onChange={(e) => setCreateAutoMis(e.target.checked)}
                className="mt-0.5 shrink-0"
              />
              <span>After saving, build last month&apos;s billing draft (recommended).</span>
            </label>

            <button
              type="submit"
              disabled={
                createSiteBusy ||
                !selectedRecon?.client_site_key?.trim() ||
                !newSiteKey.trim() ||
                !newSiteBillingClientId.trim()
              }
              className="w-full sm:w-auto px-5 py-2.5 rounded-lg text-sm font-semibold bg-[var(--accent-green)] text-white hover:brightness-95 disabled:opacity-60 transition-colors shadow-sm"
            >
              {createSiteBusy ? "Saving…" : "Save new site"}
            </button>
          </form>
        )}
      </div>

      {error ? (
        <div className="rounded-xl border border-red-300/60 bg-red-50/80 px-4 py-2.5 text-sm text-red-800">{error}</div>
      ) : null}

      {resolveMode === "map" && (
        <form onSubmit={onSubmit} className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 space-y-3">
          <h2 className="text-sm font-semibold text-[var(--text-primary)]">Link the name to a site</h2>
          <p className="text-xs text-[var(--text-muted)] leading-relaxed">
            Confirm the attendance name and pick which site it belongs to. Then save — we remove the item from the list
            above and can refresh the billing draft.
          </p>
          {selectedRecon && (
            <div className="rounded-md bg-[var(--bg-elev)] border border-[var(--border)] px-3 py-2 text-sm">
              <span className="text-[var(--text-muted)] text-xs uppercase tracking-wide font-semibold">Name from list</span>
              <p className="font-medium text-[var(--text-primary)] mt-0.5">{selectedRecon.client_site_key}</p>
            </div>
          )}
          <div>
            <label className="block text-xs font-medium text-[var(--text-primary)] mb-1">Which site?</label>
            <select
              value={selectedSiteId}
              onChange={(e) => setSelectedSiteId(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
            >
              <option value="">Choose a site…</option>
              {items.map((it) => (
                <option key={it.id} value={it.id}>
                  {it.billing_client_name} - {it.site_key} - {it.display_name || it.canonical_name}
                </option>
              ))}
            </select>
          </div>

          {selectedSite && (
            <div className="text-xs text-[var(--text-muted)]">
              Internal ID: <span className="font-mono text-[var(--text-secondary)]">{selectedSite.id}</span>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-[var(--text-muted)] mb-1">Attendance name (must match file)</label>
              <input
                value={aliasCode}
                onChange={(e) => setAliasCode(e.target.value)}
                className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
                placeholder="As it appears in attendance"
              />
            </div>
            <div>
              <label className="block text-xs text-[var(--text-muted)] mb-1">Friendly label (optional)</label>
              <input
                value={aliasDisplay}
                onChange={(e) => setAliasDisplay(e.target.value)}
                className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
                placeholder="Shown in lists and reports"
              />
            </div>
            <div>
              <label className="block text-xs text-[var(--text-muted)] mb-1">Source (usually leave as manual)</label>
              <input
                value={sourceSystem}
                onChange={(e) => setSourceSystem(e.target.value)}
                className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
              />
            </div>
            <label className="flex items-start gap-2 text-sm leading-snug">
              <input
                type="checkbox"
                checked={autoRerun}
                onChange={(e) => setAutoRerun(e.target.checked)}
                className="mt-0.5 shrink-0"
              />
              <span>After saving, build last month&apos;s billing draft</span>
            </label>
            <label className="flex items-start gap-2 text-sm leading-snug">
              <input
                type="checkbox"
                checked={useReconPeriod}
                onChange={(e) => setUseReconPeriod(e.target.checked)}
                className="mt-0.5 shrink-0"
              />
              <span>Use the same month as the open item (instead of last month)</span>
            </label>
            <label className="flex items-start gap-2 text-sm leading-snug">
              <input
                type="checkbox"
                checked={allowAliasReassign}
                onChange={(e) => setAllowAliasReassign(e.target.checked)}
                className="mt-0.5 shrink-0"
              />
              <span>This name is already linked elsewhere — move it to the site above</span>
            </label>
            <div>
              <label className="block text-xs text-[var(--text-muted)] mb-1">Note for audit (optional)</label>
              <input
                value={resolutionNotes}
                onChange={(e) => setResolutionNotes(e.target.value)}
                className="w-full px-3 py-2 rounded-lg bg-[var(--bg-elev)] border border-[var(--border)] text-sm"
                placeholder="Why you made this link"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={submitting || !selectedRecon || !selectedSiteId}
            className={`w-full sm:w-auto px-5 py-2.5 rounded-lg text-sm font-semibold transition-colors shadow-sm ${
              submitting
                ? "bg-[var(--accent-blue)] text-white cursor-wait"
                : "bg-[var(--accent-green)] text-white hover:brightness-95"
            } disabled:opacity-60`}
          >
            {submitting ? "Saving…" : "Save link & refresh billing"}
          </button>
        </form>
      )}

      {createSiteResult && (
        <div className="bg-[var(--bg-card)] border border-emerald-200/90 rounded-xl p-4 shadow-sm">
          <div className="text-base font-semibold text-emerald-900 mb-1">New site saved</div>
          <p className="text-xs text-[var(--text-muted)] mb-3">You can review billing or pick the next item in the list.</p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div className="rounded-lg border border-[var(--border)] p-3">
              <div className="text-xs text-[var(--text-muted)] mb-1">New Site</div>
              <div className="font-semibold">{String(createSiteResult.display_name ?? createSiteResult.site_key ?? "-")}</div>
              <div className="text-xs text-[var(--text-muted)] mt-1">
                ID: <span className="font-mono">{String(createSiteResult.service_site_id ?? "-")}</span>
              </div>
            </div>
            <div className="rounded-lg border border-[var(--border)] p-3">
              <div className="text-xs text-[var(--text-muted)] mb-1">Attendance name linked</div>
              <div>{String(createSiteResult.alias_code ?? "-")}</div>
            </div>
            {createSiteResult.rate_line_clone != null ? (
              <div className="rounded-lg border border-[var(--border)] p-3 md:col-span-2">
                <div className="text-xs text-[var(--text-muted)] mb-1">Rates copied from template</div>
                <div className="text-sm">
                  Copied{" "}
                  <span className="font-semibold tabular-nums">
                    {String((createSiteResult.rate_line_clone as Record<string, unknown>).cloned_count ?? "0")}
                  </span>{" "}
                  site-specific line(s) from the template.
                </div>
                {(createSiteResult.rate_line_clone as Record<string, unknown>).message ? (
                  <p className="text-xs text-amber-800 mt-1">
                    {String((createSiteResult.rate_line_clone as Record<string, unknown>).message)}
                  </p>
                ) : null}
              </div>
            ) : null}
            {!!createSiteResult.mis_rerun ? (
              <div className="rounded-lg border border-[var(--border)] p-3 md:col-span-2">
                <div className="text-xs text-[var(--text-muted)] mb-1">Billing draft</div>
                <div>
                  Status:{" "}
                  <span className="font-medium">
                    {String((createSiteResult.mis_rerun as Record<string, unknown>).status ?? "-")}
                  </span>
                  {(createSiteResult.mis_rerun as Record<string, unknown>).error ? (
                    <span className="ml-2 text-red-500">
                      {String((createSiteResult.mis_rerun as Record<string, unknown>).error)}
                    </span>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
          <div className="mt-3 pt-3 border-t border-[var(--border)]">
            <Link
              href="/admin/o2c-contract-review"
              className="text-xs font-semibold text-[var(--accent-blue)] hover:underline"
            >
              Open Contract Health →
            </Link>
            <span className="text-[10px] text-[var(--text-muted)] ml-2">Adjust cloned or contract-wide lines</span>
          </div>
        </div>
      )}

      {result && (
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-base font-semibold text-[var(--text-primary)]">Link saved</div>
            <button
              type="button"
              onClick={() => setShowRawResult((v) => !v)}
              className="px-2 py-1 text-xs rounded border border-[var(--border)] hover:bg-white/5 text-[var(--text-muted)]"
            >
              {showRawResult ? "Hide technical details" : "Technical details (support)"}
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div className="rounded-lg border border-[var(--border)] p-3">
              <div className="text-xs text-[var(--text-muted)] mb-1">Result</div>
              <div className="font-semibold">{String(result.status ?? "-")}</div>
              {showRawResult ? (
                <div className="text-xs text-[var(--text-muted)] mt-2 font-mono break-all">
                  Item ref: {String(result.recon_id ?? "-")}
                </div>
              ) : null}
            </div>
            <div className="rounded-lg border border-[var(--border)] p-3">
              <div className="text-xs text-[var(--text-muted)] mb-1">Name linked</div>
              <div>{String((result.alias as Record<string, unknown> | undefined)?.alias_code ?? "-")}</div>
              {showRawResult ? (
                <div className="text-xs text-[var(--text-muted)] mt-1 font-mono break-all">
                  Site ref:{" "}
                  {String((result.alias as Record<string, unknown> | undefined)?.service_site_id ?? "-")}
                </div>
              ) : (
                <p className="text-xs text-[var(--text-muted)] mt-1">The attendance name now points at the site you chose.</p>
              )}
            </div>
            <div className="rounded-lg border border-[var(--border)] p-3 md:col-span-2">
              <div className="text-xs text-[var(--text-muted)] mb-1">Billing draft</div>
              {result.mis_rerun ? (
                <div className="space-y-1">
                  <div>
                    Started:{" "}
                    <span className="font-medium">
                      {(result.mis_rerun as Record<string, unknown>).attempted ? "Yes" : "No"}
                    </span>
                  </div>
                  <div>
                    Status:{" "}
                    <span className="font-medium">
                      {String((result.mis_rerun as Record<string, unknown>).status ?? "-")}
                    </span>
                  </div>
                  <div>
                    Note:{" "}
                    <span className="font-medium">
                      {String(
                        (result.mis_rerun as Record<string, unknown>).reason ??
                          (result.mis_rerun as Record<string, unknown>).error ??
                          "—"
                      )}
                    </span>
                  </div>
                  {showRawResult ? (
                    <>
                      <div className="text-xs text-[var(--text-muted)] font-mono break-all">
                        Run ref: {String((result.mis_rerun as Record<string, unknown>).mis_run_id ?? "-")}
                      </div>
                      <div className="text-xs text-[var(--text-muted)] break-all">
                        File: {String((result.mis_rerun as Record<string, unknown>).xlsx_path ?? "-")}
                      </div>
                    </>
                  ) : null}
                </div>
              ) : (
                <div className="text-[var(--text-muted)]">No billing draft step was returned for this save.</div>
              )}
            </div>
          </div>
          {showRawResult && (
            <pre className="mt-3 text-xs overflow-auto whitespace-pre-wrap">
              {JSON.stringify(result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

