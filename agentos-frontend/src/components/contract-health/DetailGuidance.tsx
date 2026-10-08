"use client";

import type { ReactNode } from "react";
import Link from "next/link";

import { formatDateRange, statusBadgeClass, statusLabel } from "./statusUi";

type Sibling = {
  contract_terms_version_id: string;
  title: string | null;
  status: string;
  effective_from: string | null;
  effective_to: string | null;
  overlaps_period?: boolean;
};

type Props = {
  status: string;
  periodStart: string;
  periodEnd: string;
  isMisPick: boolean;
  siteOverlap: boolean;
  isGlobalBucket: boolean;
  siteName: string | null;
  siblings: Sibling[];
  onSelectSibling: (id: string) => void;
  onExpireClick: () => void;
};

export function DetailGuidance({
  status,
  periodStart,
  periodEnd,
  isMisPick,
  siteOverlap,
  isGlobalBucket,
  siteName,
  siblings,
  onSelectSibling,
  onExpireClick,
}: Props) {
  const st = status.toLowerCase();
  const approvedSiblings = siblings.filter((s) => String(s.status).toLowerCase() === "approved" && s.overlaps_period);
  const hasApprovedSibling = approvedSiblings.length > 0 && st === "pending";

  let tone: "info" | "warn" | "ok" = "info";
  let title = "What this contract is";
  let body: ReactNode = null;

  if (siteOverlap) {
    tone = "warn";
    title = "Overlap — MIS is blocked for this site";
    body = (
      <>
        Multiple <strong>approved</strong> contracts cover {periodStart}–{periodEnd}. Open each approved version
        and <strong>Expire</strong> the one that should not bill, then run MIS again.
      </>
    );
  } else if (hasApprovedSibling) {
    tone = "warn";
    title = "This pending contract is not used for MIS";
    body = (
      <>
        An <strong>approved</strong> sibling already covers this period. MIS will bill the approved contract,
        not this pending one. To switch: Expire the approved sibling, then complete MIS Save &amp; Approve on
        this contract.
      </>
    );
  } else if (isMisPick && st === "approved") {
    tone = "ok";
    title = "This is the contract MIS uses";
    body = (
      <>
        For {siteName || "this site"}, billing for {periodStart}–{periodEnd} is driven by this approved
        contract. Amount changes happen on the MIS page after you open a draft.
      </>
    );
  } else if (isMisPick && (st === "pending" || st === "draft")) {
    tone = "ok";
    title = "MIS will use this contract (not yet approved)";
    body = (
      <>
        This is the only billable contract for the period. Fix data issues below, run MIS, then{" "}
        <strong>Save &amp; Approve</strong> on MIS to promote it to approved.
      </>
    );
  } else if (isGlobalBucket) {
    tone = "info";
    title = "Client-wide contract (global rate lines)";
    body = (
      <>
        Lines here apply to <strong>all sites</strong> when no site-specific line exists. Site billing still
        uses each site&apos;s own contract stack on the left — global contracts rarely replace site picks.
      </>
    );
  } else if (st === "approved" && !isMisPick) {
    tone = "info";
    title = "Approved but not in this billing period";
    body = (
      <>Effective dates may fall outside {periodStart}–{periodEnd}, or another contract wins for this site.</>
    );
  } else {
    body = (
      <>
        Prepare rates and dates here. Final commercial sign-off is only on{" "}
        <Link href="/o2c/ohc-mis" className="text-[var(--accent-blue)] underline">
          OHC MIS
        </Link>
        .
      </>
    );
  }

  const boxClass =
    tone === "warn"
      ? "border-amber-300 bg-amber-50 text-amber-950"
      : tone === "ok"
        ? "border-emerald-300 bg-emerald-50 text-emerald-950"
        : "border-[var(--border)] bg-[var(--bg-elev)] text-[var(--text-secondary)]";

  return (
    <div className={`rounded-lg border p-3 text-sm ${boxClass}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold border ${statusBadgeClass(status)}`}>
          {statusLabel(status)}
        </span>
        <span className="font-semibold">{title}</span>
      </div>
      <p className="text-xs leading-relaxed">{body}</p>
      {hasApprovedSibling && !siteOverlap && (
        <ul className="mt-2 text-xs space-y-1">
          {approvedSiblings.map((s) => (
            <li key={s.contract_terms_version_id} className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="text-[var(--accent-blue)] underline"
                onClick={() => onSelectSibling(s.contract_terms_version_id)}
              >
                {s.title || "Approved sibling"}
              </button>
              <span className="text-[var(--text-muted)]">{formatDateRange(s.effective_from, s.effective_to)}</span>
            </li>
          ))}
        </ul>
      )}
      {siteOverlap && (
        <button
          type="button"
          onClick={onExpireClick}
          className="mt-2 text-xs font-semibold text-amber-900 underline"
        >
          Select a contract to Expire →
        </button>
      )}
    </div>
  );
}
