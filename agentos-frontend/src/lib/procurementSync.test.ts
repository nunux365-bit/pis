import { describe, expect, it } from "vitest";
import {
  procurementAttachmentStorage,
  ticketProcurementAttachmentSyncInFlight,
  ticketProcurementSapFormSyncInFlight,
  ticketProcurementSyncInFlight,
  type ProcurementTicket,
} from "./procurementApi";

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

describe("ticketProcurementSapFormSyncInFlight", () => {
  it("blocks save while initial SAP create is running", () => {
    const t = ticket({
      sap_id: null,
      sap_sync: { sync_pending: true, attempt_count: 1 },
    });
    expect(ticketProcurementSapFormSyncInFlight(t)).toBe(true);
  });

  it("allows save while only attachments are uploading", () => {
    const t = ticket({
      sap_sync: { attachments_pending: true },
      attachments: [{ id: "a1", name: "x.pdf", sap_document_id: null }],
    });
    expect(ticketProcurementAttachmentSyncInFlight(t)).toBe(true);
    expect(ticketProcurementSapFormSyncInFlight(t)).toBe(false);
    expect(ticketProcurementSyncInFlight(t)).toBe(true);
  });

  it("does not block save when attachments failed terminally", () => {
    const t = ticket({
      sap_sync: { attachments_pending: false, attachment_last_error: "note.pdf: timeout" },
      attachments: [
        { id: "a1", name: "note.pdf", sap_sync_error: "timeout", can_retry_upload: false },
      ],
    });
    expect(ticketProcurementAttachmentSyncInFlight(t)).toBe(false);
    expect(ticketProcurementSapFormSyncInFlight(t)).toBe(false);
  });

  it("does not treat stale attachments_pending as in flight when rows are terminal", () => {
    const t = ticket({
      sap_sync: { attachments_pending: true, attachment_last_error: "note.pdf: timeout" },
      attachments: [{ id: "a1", name: "note.pdf", sap_sync_error: "timeout", can_retry_upload: false }],
    });
    expect(ticketProcurementAttachmentSyncInFlight(t)).toBe(false);
    expect(ticketProcurementSapFormSyncInFlight(t)).toBe(false);
  });

  it("treats backoff retry as pending when can_retry_upload is true", () => {
    const t = ticket({
      attachments: [
        { id: "a1", name: "note.pdf", sap_sync_error: "timeout", can_retry_upload: true },
      ],
    });
    expect(ticketProcurementAttachmentSyncInFlight(t)).toBe(true);
  });

  it("blocks save during SAP resubmit when attachments are not pending", () => {
    const t = ticket({
      sap_sync: { sync_pending: true, attachments_pending: false },
      attachments: [{ id: "a1", name: "x.pdf", sap_document_id: "SAP-1" }],
    });
    expect(ticketProcurementSapFormSyncInFlight(t)).toBe(true);
  });
});

describe("procurementAttachmentStorage", () => {
  it("classifies sap vs local", () => {
    expect(procurementAttachmentStorage({ storage: "sap", name: "a" })).toBe("sap");
    expect(procurementAttachmentStorage({ storage: "local", name: "a" })).toBe("local");
    expect(procurementAttachmentStorage({ sap_document_id: "FOL-1" })).toBe("sap");
    expect(procurementAttachmentStorage({ id: "a1", name: "x.pdf" })).toBe("local");
  });
});
