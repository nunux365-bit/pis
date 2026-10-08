import Link from "next/link";
import type { LinkedTicketSummary, ProcurementTicket } from "@/lib/procurementApi";

function linkLabel(t: LinkedTicketSummary): string {
  const sap = (t.sap_id ?? "").trim();
  if (sap) return sap;
  return t.kind === "PO" ? "Purchase order" : "Purchase request";
}

export function TicketLinksListCell({ ticket }: { ticket: ProcurementTicket }) {
  if (ticket.kind === "PO" && ticket.parent_pr_summary) {
    const pr = ticket.parent_pr_summary;
    return (
      <Link
        href={`/procurement/tickets/${pr.id}`}
        className="text-xs font-semibold text-[var(--accent-blue)] hover:underline"
        onClick={(e) => e.stopPropagation()}
      >
        From PR {linkLabel(pr)}
      </Link>
    );
  }
  if (ticket.kind === "PR" && ticket.linked_pos && ticket.linked_pos.length > 0) {
    const pos = ticket.linked_pos;
    if (pos.length === 1) {
      return (
        <Link
          href={`/procurement/tickets/${pos[0]!.id}`}
          className="text-xs font-semibold text-[var(--accent-blue)] hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          PO {linkLabel(pos[0]!)}
        </Link>
      );
    }
    return (
      <span className="text-xs text-[var(--text-secondary)]">
        {pos.length} linked orders
        <span className="mt-0.5 block font-mono text-[10px] text-[var(--text-muted)]">
          {pos
            .slice(0, 2)
            .map((p) => linkLabel(p))
            .join(" · ")}
          {pos.length > 2 ? " …" : ""}
        </span>
      </span>
    );
  }
  return <span className="text-xs text-[var(--text-muted)]">—</span>;
}

export function TicketLinksDetailPanel({ ticket }: { ticket: ProcurementTicket }) {
  const hasParent = ticket.kind === "PO" && ticket.parent_pr_summary;
  const linkedPos = ticket.kind === "PR" ? ticket.linked_pos ?? [] : [];
  if (!hasParent && linkedPos.length === 0) return null;

  return (
    <div className="relative mt-3 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]/40 px-3 py-2.5">
      <p className="text-[11px] font-bold uppercase tracking-wide text-[var(--text-muted)]">Related tickets</p>
      {hasParent && ticket.parent_pr_summary ? (
        <div className="mt-2">
          <p className="text-xs text-[var(--text-secondary)]">Created from purchase request</p>
          <Link
            href={`/procurement/tickets/${ticket.parent_pr_summary.id}`}
            className="mt-1 inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--accent-blue)] hover:underline"
          >
            <span className="font-mono">{linkLabel(ticket.parent_pr_summary)}</span>
            <span className="text-xs font-normal text-[var(--text-muted)]">({ticket.parent_pr_summary.document_type})</span>
          </Link>
        </div>
      ) : null}
      {linkedPos.length > 0 ? (
        <div className={hasParent ? "mt-3 border-t border-[var(--border)]/70 pt-3" : "mt-2"}>
          <p className="text-xs text-[var(--text-secondary)]">
            {linkedPos.length === 1 ? "Linked purchase order" : `${linkedPos.length} linked purchase orders`}
          </p>
          <ul className="mt-1.5 space-y-1">
            {linkedPos.map((po) => (
              <li key={po.id}>
                <Link
                  href={`/procurement/tickets/${po.id}`}
                  className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--accent-blue)] hover:underline"
                >
                  <span className="font-mono">{linkLabel(po)}</span>
                  <span className="text-xs font-normal text-[var(--text-muted)]">({po.document_type})</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
