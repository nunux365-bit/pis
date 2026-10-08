import { describe, expect, it } from "vitest";
import { ticketSapListStatus } from "./procurementTicketUi";
import type { ProcurementTicket } from "./procurementApi";

function ticket(partial: Partial<ProcurementTicket>): ProcurementTicket {
  return {
    id: "t1",
    kind: "PR",
    document_type: "YSER",
    form: { header: {}, lines: [] },
    attachments: [],
    sap_id: "1010000999",
    sap_sync: {},
    version: 1,
    created_at: "",
    updated_at: "",
    ...partial,
  };
}

describe("ticketSapListStatus", () => {
  it("shows attachment failed when doc is in SAP but a file failed", () => {
    const t = ticket({
      attachments: [{ id: "a1", name: "x.pdf", sap_sync_error: "timeout" }],
    });
    expect(ticketSapListStatus(t, 6).label).toBe("Attachment failed");
  });

  it("shows uploading when attachments are still pending", () => {
    const t = ticket({
      sap_sync: { attachments_pending: true },
      attachments: [{ id: "a1", name: "x.pdf" }],
    });
    expect(ticketSapListStatus(t, 6).label).toBe("Uploading files…");
  });

  it("shows in SAP when doc and attachments are healthy", () => {
    const t = ticket({
      attachments: [{ id: "a1", name: "x.pdf", sap_document_id: "DOC-1" }],
    });
    expect(ticketSapListStatus(t, 6).label).toBe("In SAP");
  });

  it("shows in SAP when attachments_pending flag is stale but rows are terminal", () => {
    const t = ticket({
      sap_sync: { attachments_pending: true },
      attachments: [{ id: "a1", name: "x.pdf", sap_sync_error: "timeout", can_retry_upload: false }],
    });
    expect(ticketSapListStatus(t, 6).label).toBe("Attachment failed");
  });
});
