"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  getO2CContractReviewItem,
  getO2CContractReviewSiblings,
  patchO2CContractRateLines,
  patchO2CContractReviewHeader,
  setO2CContractReviewStatus,
  type ContractRateLinePatchItem,
} from "@/lib/api";

import { ConfirmDialog } from "./ConfirmDialog";
import { DetailGuidance } from "./DetailGuidance";
import { canSafelyExpireCtv } from "./expireSafety";
import { statusLabel } from "./statusUi";
import type { ClientTreeSite } from "./siteUtils";

export type CtvDetailContext = {
  isGlobalBucket: boolean;
  siteName: string | null;
  siteOverlap: boolean;
  pickerCtvId: string | null;
};

type Props = {
  ctvId: string;
  periodStart: string;
  periodEnd: string;
  context: CtvDetailContext;
  site?: ClientTreeSite | null;
  onUpdated: () => void;
  onSelectSibling: (ctvId: string) => void;
};

type LineDraft = {
  rate_amount: string;
  role_code: string;
  description: string;
  billing_model: string;
  is_active: boolean;
};

const BILLING_MODELS = [
  "fixed_monthly",
  "rate_attendance",
  "as_per_actuals",
  "per_visit",
  "per_head",
] as const;

function siteColumnLabel(
  rl: Record<string, unknown>,
  ctx: CtvDetailContext
): { label: string; hint?: string } {
  if (rl.service_site_id) {
    return { label: String(rl.service_site_name ?? rl.service_site_key ?? "Site") };
  }
  if (ctx.isGlobalBucket) {
    return { label: "All sites", hint: "global line" };
  }
  return { label: "—", hint: "needs site link" };
}

export function CtvDetailPanel({
  ctvId,
  periodStart,
  periodEnd,
  context,
  site,
  onUpdated,
  onSelectSibling,
}: Props) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof getO2CContractReviewItem>> | null>(null);
  const [siblings, setSiblings] = useState<Awaited<ReturnType<typeof getO2CContractReviewSiblings>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [efFrom, setEfFrom] = useState("");
  const [efTo, setEfTo] = useState("");
  const [openEnded, setOpenEnded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editLineId, setEditLineId] = useState<string | null>(null);
  const [lineDraft, setLineDraft] = useState<LineDraft | null>(null);
  const [confirmAction, setConfirmAction] = useState<"expired" | "rejected" | null>(null);
  const loadGen = useRef(0);

  const load = useCallback(async () => {
    const gen = ++loadGen.current;
    setLoading(true);
    setError("");
    try {
      const [d, s] = await Promise.all([
        getO2CContractReviewItem(ctvId),
        getO2CContractReviewSiblings(ctvId, periodStart, periodEnd),
      ]);
      if (gen !== loadGen.current) return;
      setDetail(d);
      setSiblings(s);
      const h = d.header as Record<string, unknown>;
      setEfFrom(String(h.effective_from ?? "").slice(0, 10));
      const to = h.effective_to ? String(h.effective_to).slice(0, 10) : "";
      setEfTo(to);
      setOpenEnded(!to);
    } catch (e) {
      if (gen !== loadGen.current) return;
      setError(e instanceof Error ? e.message : "Failed to load");
      setDetail(null);
    } finally {
      if (gen === loadGen.current) setLoading(false);
    }
  }, [ctvId, periodStart, periodEnd]);

  useEffect(() => {
    setEditLineId(null);
    setLineDraft(null);
    void load();
  }, [load]);

  const header = (detail?.header ?? {}) as Record<string, unknown>;
  const status = String(header.status ?? "").toLowerCase();
  const isApproved = status === "approved";
  const lines = (detail?.rate_lines ?? []) as Record<string, unknown>[];
  const docs = (detail?.documents ?? []) as Record<string, unknown>[];
  const extraction = (detail?.latest_extraction_run ?? {}) as Record<string, unknown>;

  const expireCheck = canSafelyExpireCtv(site ?? null, ctvId);

  async function applyStatus(statusValue: "expired" | "rejected") {
    if (statusValue === "expired" && !expireCheck.safe) {
      setError(expireCheck.reason ?? "Cannot expire this contract for the billing month.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await setO2CContractReviewStatus({
        contract_terms_version_id: ctvId,
        status: statusValue,
        ...(statusValue === "expired"
          ? { period_start: periodStart, period_end: periodEnd }
          : {}),
      });
      setConfirmAction(null);
      onUpdated();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Status update failed");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveDates() {
    if (isApproved) return;
    if (!efFrom) {
      setError("effective_from is required");
      return;
    }
    if (!openEnded && !efTo) {
      setError("Set an end date or check Open-ended");
      return;
    }
    if (!openEnded && efFrom > efTo) {
      setError("effective_from must be on or before effective_to");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await patchO2CContractReviewHeader(ctvId, {
        effective_from: efFrom,
        ...(openEnded
          ? { clear_effective_to: true }
          : { effective_to: efTo || undefined, clear_effective_to: false }),
      });
      onUpdated();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save dates failed");
    } finally {
      setBusy(false);
    }
  }

  function startEditLine(rl: Record<string, unknown>) {
    const id = String(rl.id ?? "");
    setEditLineId(id);
    setLineDraft({
      rate_amount: String(rl.rate_amount ?? ""),
      role_code: String(rl.role_code ?? ""),
      description: String(rl.description ?? ""),
      billing_model: String(rl.billing_model ?? "fixed_monthly"),
      is_active: rl.is_active !== false,
    });
  }

  async function onSaveLine() {
    if (!editLineId || !lineDraft) return;
    const rate = lineDraft.rate_amount.trim();
    const patch: ContractRateLinePatchItem = {
      id: editLineId,
      role_code: lineDraft.role_code.trim() || undefined,
      description: lineDraft.description.trim() || undefined,
      billing_model: lineDraft.billing_model || undefined,
      is_active: lineDraft.is_active,
      rate_amount: rate === "" ? undefined : Number(rate),
    };
    if (patch.rate_amount !== undefined && Number.isNaN(patch.rate_amount)) {
      setError("Invalid rate amount");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await patchO2CContractRateLines(ctvId, [patch]);
      setEditLineId(null);
      setLineDraft(null);
      onUpdated();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save line failed");
    } finally {
      setBusy(false);
    }
  }

  if (loading && !detail) {
    return <p className="text-sm text-[var(--text-muted)]">Loading…</p>;
  }
  if (!detail) {
    return <p className="text-sm text-[var(--text-muted)]">{error || "Select a contract term"}</p>;
  }

  const isPicker = context.pickerCtvId === ctvId;
  const title = String(header.title ?? "Untitled");
  const clientName = String(header.client_name ?? "");
  const siblingItems = siblings?.items ?? [];

  return (
    <div className="space-y-4 border border-[var(--border)] rounded-xl p-4 bg-[var(--bg-card)]">
      <ConfirmDialog
        open={confirmAction === "expired"}
        title="Expire contract terms?"
        message={`Expire "${title}" (${clientName})?\n\nUse this to resolve overlap (multiple approved terms). MIS will not pick expired terms. This cannot be undone from the UI.`}
        confirmLabel="Expire"
        variant="warning"
        busy={busy}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => void applyStatus("expired")}
      />
      <ConfirmDialog
        open={confirmAction === "rejected"}
        title="Reject contract terms?"
        message={`Reject "${title}" (${clientName})?\n\nUse for bad ingest / wrong document. MIS will not bill from rejected terms.`}
        confirmLabel="Reject"
        variant="danger"
        busy={busy}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => void applyStatus("rejected")}
      />

      {error && <p className="text-sm text-red-500">{error}</p>}

      <DetailGuidance
        status={status}
        periodStart={periodStart}
        periodEnd={periodEnd}
        isMisPick={isPicker}
        siteOverlap={context.siteOverlap}
        isGlobalBucket={context.isGlobalBucket}
        siteName={context.siteName}
        siblings={siblingItems}
        onSelectSibling={onSelectSibling}
        onExpireClick={() => setConfirmAction("expired")}
      />

      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-lg font-semibold">{title}</h3>
          <p className="text-xs text-[var(--text-muted)]">
            {clientName}
            {context.siteName && !context.isGlobalBucket && (
              <span> · {context.siteName}</span>
            )}
            {context.isGlobalBucket && <span> · Shared (all sites)</span>}
            <span> · {statusLabel(status)}</span>
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy || !expireCheck.safe}
            title={expireCheck.safe ? undefined : expireCheck.reason}
            onClick={() => setConfirmAction("expired")}
            className="px-2 py-1 text-xs rounded border border-amber-500 text-amber-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Expire
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setConfirmAction("rejected")}
            className="px-2 py-1 text-xs rounded border border-red-400 text-red-500 disabled:opacity-50"
          >
            Reject
          </button>
          <Link
            href="/o2c/ohc-mis"
            className="px-2 py-1 text-xs rounded border border-[var(--accent-green)] text-[var(--accent-green)]"
          >
            Open MIS →
          </Link>
        </div>
      </div>

      {!expireCheck.safe && (
        <p className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded-lg px-2 py-1.5">
          {expireCheck.reason}
        </p>
      )}

      <p className="text-[11px] text-[var(--text-muted)]">
        Commercial approval is via MIS Save &amp; Approve only. Lines that need a site link:{" "}
        <Link href="/admin/o2c-site-alias" className="text-[var(--accent-blue)] hover:underline">
          Site alias
        </Link>
        .
      </p>

      <div className="grid grid-cols-2 gap-2 max-w-md">
        <label className="text-xs">
          <span className="text-[var(--text-muted)]">effective_from</span>
          <input
            type="date"
            value={efFrom}
            disabled={isApproved || busy}
            onChange={(e) => setEfFrom(e.target.value)}
            className="w-full mt-0.5 px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-elev)] disabled:opacity-50"
          />
        </label>
        <label className="text-xs">
          <span className="text-[var(--text-muted)]">effective_to</span>
          <input
            type="date"
            value={efTo}
            disabled={isApproved || openEnded || busy}
            onChange={(e) => setEfTo(e.target.value)}
            className="w-full mt-0.5 px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-elev)] disabled:opacity-50"
          />
        </label>
        <label className="col-span-2 flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={openEnded}
            disabled={isApproved || busy}
            onChange={(e) => {
              setOpenEnded(e.target.checked);
              if (e.target.checked) setEfTo("");
            }}
          />
          Open-ended (no end date)
        </label>
        {isApproved ? (
          <p className="col-span-2 text-[11px] text-amber-700">
            Approved terms: edit dates via MIS or Expire this version first.
          </p>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => void onSaveDates()}
            className="col-span-2 px-2 py-1 text-xs rounded bg-[var(--accent-blue)] text-white disabled:opacity-50"
          >
            Save dates
          </button>
        )}
      </div>

      {docs[0] && (
        <p className="text-xs">
          Source: <span className="font-medium">{String(docs[0].original_filename ?? "-")}</span>
          {extraction.needs_human_review === true && (
            <span className="ml-2 text-amber-600">· Needs extraction review</span>
          )}
        </p>
      )}

      {siblingItems.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold mb-1">Other contracts at this site</h4>
          <ul className="text-xs space-y-1">
            {siblingItems.map((s) => (
              <li key={s.contract_terms_version_id}>
                <button
                  type="button"
                  className="text-left text-[var(--accent-blue)] hover:underline"
                  onClick={() => onSelectSibling(s.contract_terms_version_id)}
                >
                  {s.title || s.contract_terms_version_id.slice(0, 8)} · {statusLabel(String(s.status))}
                  {s.overlaps_period ? " · overlaps billing month" : ""}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h4 className="text-sm font-semibold mb-2">Rate lines ({lines.length})</h4>
        {editLineId && lineDraft && (
          <div className="mb-3 p-3 border border-[var(--accent-blue)] rounded-lg space-y-2 bg-[rgba(11,116,222,0.05)]">
            <p className="text-xs font-semibold">Edit line</p>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-xs col-span-2">
                Role
                <input
                  value={lineDraft.role_code}
                  onChange={(e) => setLineDraft({ ...lineDraft, role_code: e.target.value })}
                  className="w-full mt-0.5 px-2 py-1 border rounded text-xs"
                />
              </label>
              <label className="text-xs col-span-2">
                Description
                <input
                  value={lineDraft.description}
                  onChange={(e) => setLineDraft({ ...lineDraft, description: e.target.value })}
                  className="w-full mt-0.5 px-2 py-1 border rounded text-xs"
                />
              </label>
              <label className="text-xs">
                Billing model
                <select
                  value={lineDraft.billing_model}
                  onChange={(e) => setLineDraft({ ...lineDraft, billing_model: e.target.value })}
                  className="w-full mt-0.5 px-2 py-1 border rounded text-xs"
                >
                  {BILLING_MODELS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs">
                Rate amount
                <input
                  type="number"
                  step="0.01"
                  value={lineDraft.rate_amount}
                  onChange={(e) => setLineDraft({ ...lineDraft, rate_amount: e.target.value })}
                  className="w-full mt-0.5 px-2 py-1 border rounded text-xs"
                />
              </label>
              <label className="col-span-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={lineDraft.is_active}
                  onChange={(e) => setLineDraft({ ...lineDraft, is_active: e.target.checked })}
                />
                Line active
              </label>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void onSaveLine()}
                className="px-2 py-1 text-xs rounded bg-[var(--accent-blue)] text-white disabled:opacity-50"
              >
                Save line
              </button>
              <button
                type="button"
                onClick={() => {
                  setEditLineId(null);
                  setLineDraft(null);
                }}
                className="px-2 py-1 text-xs rounded border"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
        <div className="max-h-64 overflow-auto border border-[var(--border)] rounded-lg">
          <table className="w-full text-xs">
            <thead className="bg-[var(--bg-elev)] sticky top-0">
              <tr>
                <th className="text-left p-2">Role</th>
                <th className="text-left p-2">Site</th>
                <th className="text-right p-2">Rate</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {lines.map((rl) => {
                const id = String(rl.id ?? "");
                const inactive = rl.is_active === false;
                const siteCol = siteColumnLabel(rl, context);
                return (
                  <tr key={id} className={`border-t border-[var(--border)] ${inactive ? "opacity-50" : ""}`}>
                    <td className="p-2">{String(rl.role_code ?? "—")}</td>
                    <td className="p-2">
                      {siteCol.label}
                      {siteCol.hint && (
                        <span
                          className={`block text-[10px] ${
                            siteCol.hint === "needs site link" ? "text-amber-600" : "text-[var(--text-muted)]"
                          }`}
                        >
                          {siteCol.hint}
                        </span>
                      )}
                    </td>
                    <td className="p-2 text-right tabular-nums">{String(rl.rate_amount ?? "—")}</td>
                    <td className="p-2">
                      <button
                        type="button"
                        className="text-[var(--accent-blue)]"
                        disabled={!!editLineId && editLineId !== id}
                        onClick={() => startEditLine(rl)}
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
