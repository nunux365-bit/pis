import type { ProcurementTicket } from "./procurementApi";
import {
  procurementAttachmentSapStatus,
  ticketProcurementAttachmentSyncInFlight,
  ticketProcurementSyncInFlight,
} from "./procurementApi";

function ticketHasFailedAttachments(t: ProcurementTicket): boolean {
  return (t.attachments ?? []).some((a) => procurementAttachmentSapStatus(a) === "failed");
}

function ticketHasAttachmentWorkInFlight(t: ProcurementTicket): boolean {
  return Boolean((t.sap_id ?? "").trim()) && ticketProcurementAttachmentSyncInFlight(t);
}

/** AgentOS ↔ SAP sync chip (no live SAP OData). */
export function ticketSapListStatus(
  t: ProcurementTicket,
  sapMaxAttempts: number
): { label: string; tone: string } {
  const lastErr = (t.sap_sync?.last_error ?? "").trim();
  const n = t.sap_sync?.attempt_count ?? 0;
  if (lastErr) {
    if (n >= sapMaxAttempts) {
      return {
        label: "Needs help",
        tone: "bg-[rgba(217,79,69,0.12)] text-[var(--accent-red)] ring-1 ring-red-200/40",
      };
    }
    return {
      label: "SAP failed",
      tone: "bg-[rgba(229,143,34,0.14)] text-[var(--accent-orange)] ring-1 ring-orange-200/50",
    };
  }
  if (t.sap_id) {
    if (ticketHasFailedAttachments(t)) {
      return {
        label: "Attachment failed",
        tone: "bg-[rgba(217,79,69,0.12)] text-[var(--accent-red)] ring-1 ring-red-200/40",
      };
    }
    if (ticketHasAttachmentWorkInFlight(t)) {
      return {
        label: "Uploading files…",
        tone: "bg-[rgba(229,143,34,0.14)] text-[var(--accent-orange)] ring-1 ring-orange-200/50",
      };
    }
    return {
      label: "In SAP",
      tone: "bg-[rgba(21,163,115,0.14)] text-[var(--accent-green)] ring-1 ring-[var(--accent-green)]/20",
    };
  }
  if (n >= sapMaxAttempts) {
    return {
      label: "Needs help",
      tone: "bg-[rgba(217,79,69,0.12)] text-[var(--accent-red)] ring-1 ring-red-200/40",
    };
  }
  if (n > 0) {
    return {
      label: "SAP pending",
      tone: "bg-[rgba(229,143,34,0.14)] text-[var(--accent-orange)] ring-1 ring-orange-200/50",
    };
  }
  if (ticketProcurementSyncInFlight(t, sapMaxAttempts)) {
    return {
      label: "Syncing…",
      tone: "bg-[rgba(229,143,34,0.14)] text-[var(--accent-orange)] ring-1 ring-orange-200/50",
    };
  }
  return {
    label: "Submitted",
    tone: "bg-[var(--bg-elev)] text-[var(--text-secondary)] ring-1 ring-[var(--border)]",
  };
}

export type LinkedTicketSummary = {
  id: string;
  kind: string;
  sap_id: string | null;
  document_type: string;
};

export function ticketLinkLine(t: ProcurementTicket): string | null {
  if (t.kind === "PO" && t.parent_pr_summary) {
    const sap = t.parent_pr_summary.sap_id?.trim();
    return sap ? `From PR ${sap}` : "From purchase request";
  }
  if (t.kind === "PR" && t.linked_pos && t.linked_pos.length > 0) {
    const n = t.linked_pos.length;
    const first = t.linked_pos[0]?.sap_id?.trim();
    if (n === 1 && first) return `PO ${first}`;
    if (n === 1) return "1 linked order";
    return `${n} linked orders`;
  }
  return null;
}
